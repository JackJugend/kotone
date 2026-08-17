"""Offline regression tests for storage and fragile AOTY parsers.

Run locally with:
    python -m unittest tests.test_core

No test performs a real network request.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = tempfile.mkdtemp(prefix="kotone-tests-runtime-")
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ["DATA_DIR"] = TEST_RUNTIME
sys.path.insert(0, str(ROOT))

import aoty  # noqa: E402
from database import Database  # noqa: E402
from http_client import ResilientHTTPClient  # noqa: E402


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kotone-db-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_db(self, users=("enso",)):
        return Database(
            str(self.tmp / "kotone.sqlite3"),
            monitored_users=users,
            legacy_json_path=str(self.tmp / "data.json"),
            migrated_backup_path=str(self.tmp / "data_migrated.json.bak"),
            backup_path=str(self.tmp / "kotone.backup.sqlite3"),
        )

    def test_legacy_migration_only_keeps_config_users(self):
        payload = {
            "version": 4,
            "users": {
                "enso": {
                    "ratings": {
                        "1": {
                            "score": "90",
                            "artist": "A",
                            "album": "B",
                        }
                    }
                },
                "not-in-config": {
                    "ratings": {
                        "2": {
                            "score": "10",
                            "artist": "X",
                            "album": "Y",
                        }
                    }
                },
            },
        }
        (self.tmp / "data.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

        db = self.make_db(("enso",))
        self.assertIsNotNone(db.get_rating("enso", "1"))
        self.assertIsNone(db.get_rating("not-in-config", "2"))
        self.assertEqual(db.summary()["users"], 1)
        db.close()

    def test_removing_user_from_config_purges_their_persistent_data(self):
        db = self.make_db(("enso", "temporary"))
        db.upsert_rating(
            "temporary",
            {
                "album_id": "private-1",
                "score": "50",
                "artist": "X",
                "album": "Y",
            },
        )
        db.close()

        reopened = self.make_db(("enso",))
        self.assertIsNone(
            reopened.get_rating("temporary", "private-1")
        )
        self.assertEqual(reopened.summary()["users"], 1)
        reopened.close()

    def test_database_diagnostics_counts_only_configured_users(self):
        db = self.make_db(("enso",))

        db.upsert_rating(
            "enso",
            {
                "album_id": "diag-1",
                "score": "92",
                "artist": "Artist",
                "album": "Album",
                "has_review": True,
                "has_track_ratings": True,
            },
        )

        stats = db.diagnostics()

        self.assertTrue(stats["healthy"])
        self.assertEqual(stats["quick_check"], "ok")
        self.assertEqual(stats["counts"]["users"], 1)
        self.assertEqual(stats["counts"]["ratings_active"], 1)
        self.assertEqual(stats["counts"]["ratings_total"], 1)
        self.assertEqual(stats["counts"]["reviews"], 1)
        self.assertEqual(stats["counts"]["track_rating_albums"], 1)

        self.assertEqual(len(stats["users"]), 1)
        self.assertEqual(stats["users"][0]["username"], "enso")
        self.assertEqual(stats["users"][0]["ratings_total"], 1)

        db.close()

    def test_cached_artist_and_release_catalog_comes_from_ratings(self):
        db = self.make_db(("enso",))
        db.upsert_rating(
            "enso",
            {
                "album_id": "783921",
                "score": "91",
                "artist": "ARTMS",
                "artist_url": "https://www.albumoftheyear.org/artist/999-artms/",
                "album": "Dall",
                "url": "https://www.albumoftheyear.org/album/783921-artms-dall.php",
                "release_format": "LP",
            },
        )
        db.save_release_details(
            "783921",
            {
                "artist": "ARTMS",
                "album": "Dall",
                "url": "https://www.albumoftheyear.org/album/783921-artms-dall.php",
                "year": "2024",
                "release_date": "May 31, 2024",
                "album_format": "LP",
            },
        )

        artists = db.cached_artists()
        self.assertEqual(
            artists,
            [
                {
                    "name": "ARTMS",
                    "url": "https://www.albumoftheyear.org/artist/999-artms/",
                    "release_count": 1,
                }
            ],
        )

        releases = db.cached_artist_releases("artms")
        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0]["album_id"], "783921")
        self.assertEqual(releases[0]["title"], "Dall")
        self.assertEqual(releases[0]["year"], "2024")
        self.assertEqual(releases[0]["source"], "SQLite cache")
        db.close()

    def test_existing_old_sqlite_schema_upgrades_in_place(self):
        import sqlite3

        path = self.tmp / "kotone.sqlite3"
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE users (
                username TEXT PRIMARY KEY COLLATE NOCASE,
                format_monitor_version INTEGER
            );
            CREATE TABLE ratings (
                username TEXT NOT NULL COLLATE NOCASE,
                album_id TEXT NOT NULL,
                score TEXT,
                date TEXT,
                artist TEXT,
                album TEXT,
                release_format TEXT,
                has_review INTEGER NOT NULL DEFAULT 0,
                has_track_ratings INTEGER NOT NULL DEFAULT 0,
                liked INTEGER NOT NULL DEFAULT 0,
                review_url TEXT,
                PRIMARY KEY (username, album_id)
            );
            INSERT INTO users(username) VALUES('enso');
            INSERT INTO ratings(username, album_id, score, artist, album)
            VALUES('enso', 'old-1', '91', 'A', 'B');
            """
        )
        connection.commit()
        connection.close()

        db = self.make_db(("enso",))
        self.assertEqual(
            db.get_rating("enso", "old-1")["score"],
            "91",
        )
        self.assertIn(
            "rating_format_sync",
            {
                row[0]
                for row in db.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            },
        )
        rating_columns = {
            row[1]
            for row in db.connection.execute(
                "PRAGMA table_info(ratings)"
            ).fetchall()
        }
        self.assertIn("notify_pending", rating_columns)
        db.close()

    def test_existing_rating_history_is_imported_into_unified_history_once(self):
        import sqlite3

        path = self.tmp / "kotone.sqlite3"
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE users (
                username TEXT PRIMARY KEY COLLATE NOCASE,
                format_monitor_version INTEGER
            );
            CREATE TABLE ratings (
                username TEXT NOT NULL COLLATE NOCASE,
                album_id TEXT NOT NULL,
                score TEXT,
                date TEXT,
                artist TEXT,
                album TEXT,
                release_format TEXT,
                has_review INTEGER NOT NULL DEFAULT 0,
                has_track_ratings INTEGER NOT NULL DEFAULT 0,
                liked INTEGER NOT NULL DEFAULT 0,
                review_url TEXT,
                PRIMARY KEY (username, album_id)
            );
            CREATE TABLE rating_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE,
                album_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                old_score TEXT,
                new_score TEXT,
                changed_at REAL NOT NULL
            );
            INSERT INTO users(username) VALUES('enso');
            INSERT INTO ratings(username, album_id, score, artist, album)
            VALUES('enso', 'legacy-history', '90', 'A', 'B');
            INSERT INTO rating_history(
                username, album_id, event_type, old_score, new_score, changed_at
            ) VALUES('enso', 'legacy-history', 'score', '80', '90', 12345);
            """
        )
        connection.commit()
        connection.close()

        db = self.make_db(("enso",))
        events = db.get_change_history("enso")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "score_changed")
        self.assertEqual(events[0]["old_value"], "80")
        self.assertEqual(events[0]["new_value"], "90")
        db.close()

        # Reopening must not duplicate the imported event.
        reopened = self.make_db(("enso",))
        self.assertEqual(len(reopened.get_change_history("enso")), 1)
        reopened.close()

    def test_corrupt_database_restores_local_backup(self):
        db = self.make_db(("enso",))
        db.upsert_rating(
            "enso",
            {
                "album_id": "safe-1",
                "score": "99",
                "artist": "A",
                "album": "Safe",
            },
        )
        self.assertTrue(db.backup_if_due(force=True))
        db.close()

        database_path = self.tmp / "kotone.sqlite3"
        database_path.write_bytes(b"not a sqlite database")

        recovered = self.make_db(("enso",))
        self.assertEqual(
            recovered.get_rating("enso", "safe-1")["score"],
            "99",
        )
        recovered.close()

    def test_profile_favorites_and_rating_details_roundtrip(self):
        db = self.make_db(("enso",))
        db.save_profile(
            "enso",
            {
                "username": "enso",
                "url": "https://example/profile",
                "avatar": "https://example/avatar.jpg",
                "ratings_count": "100",
                "reviews_count": "5",
                "lists_count": "2",
                "following_count": "3",
                "followers_count": "4",
                "average_rating": 77.5,
                "average_rating_text": "~77.5",
                "rating_distribution": {"70-79": 10},
                "favorite_kind": "artists",
                "favorites": [
                    {
                        "type": "artist",
                        "name": "Artist",
                        "artist": "Artist",
                        "album": None,
                        "url": "https://example/artist",
                    }
                ],
            },
        )
        db.upsert_rating(
            "enso",
            {
                "album_id": "10",
                "score": "88",
                "date": "17.08.2026",
                "artist": "Artist",
                "album": "Album",
                "url": "https://example/album",
                "has_review": True,
                "has_track_ratings": True,
            },
        )
        db.save_rating_detail(
            "enso",
            "10",
            {
                "score": "88",
                "date": "17.08.2026",
                "has_review": True,
                "review_text": "review",
                "has_track_ratings": True,
                "track_ratings": [
                    {"number": 1, "title": "Track", "score": "91"}
                ],
                "liked": True,
                "detail_incomplete": False,
            },
        )

        profile = db.get_profile("enso", recent_limit=5)
        self.assertEqual(profile["followers_count"], "4")
        self.assertEqual(profile["favorites"][0]["name"], "Artist")
        detail = db.get_rating_detail("enso", "10")
        self.assertEqual(detail["review_text"], "review")
        self.assertEqual(detail["track_ratings"][0]["score"], "91")
        db.close()

    def test_format_archive_is_scoped_and_non_destructive_on_empty_snapshot(self):
        db = self.make_db(("enso",))

        db.upsert_format_snapshot(
            "enso",
            "Holiday",
            [
                {
                    "album_id": "holiday-1",
                    "score": "81",
                    "artist": "A",
                    "album": "Winter",
                    "release_format": "Holiday",
                }
            ],
        )

        self.assertIsNotNone(
            db.get_rating("enso", "holiday-1")
        )

        # For notification-enabled formats, archival completeness may add
        # metadata/old rows but may not overwrite the monitor's comparison
        # score before it can announce the change.
        db.upsert_rating(
            "enso",
            {
                "album_id": "lp-1",
                "score": "70",
                "artist": "A",
                "album": "LP",
                "release_format": "LP",
            },
        )
        db.upsert_format_snapshot(
            "enso",
            "LP",
            [
                {
                    "album_id": "lp-1",
                    "score": "90",
                    "artist": "A",
                    "album": "LP",
                    "release_format": "LP",
                }
            ],
            preserve_existing_state=True,
            deactivate_missing=False,
        )
        self.assertEqual(
            db.get_rating("enso", "lp-1")["score"],
            "70",
        )

        # An empty result can be a parser/site hiccup. It must not erase a
        # previously valid format snapshot.
        db.upsert_format_snapshot(
            "enso",
            "Holiday",
            [],
        )
        self.assertIsNotNone(
            db.get_rating("enso", "holiday-1")
        )

        with self.assertRaises(ValueError):
            db.upsert_format_snapshot(
                "not-in-config",
                "Holiday",
                [],
            )

        due = db.archive_due_formats(
            "enso",
            ["lp", "holiday"],
            interval=86400,
            limit=2,
        )
        self.assertEqual(due, ["lp", "holiday"])

        db.mark_format_sync(
            "enso",
            "lp",
            success=True,
            item_count=10,
        )
        due_after = db.archive_due_formats(
            "enso",
            ["lp", "holiday"],
            interval=86400,
            limit=2,
        )
        self.assertEqual(due_after, ["holiday"])
        db.close()

    def test_background_archive_new_row_can_remain_pending_for_monitor(self):
        db = self.make_db(("enso",))

        # First bootstrap is historical seeding and must not create a flood.
        db.upsert_format_snapshot(
            "enso",
            "LP",
            [
                {
                    "album_id": "old-1",
                    "score": "70",
                    "artist": "A",
                    "album": "Old",
                    "release_format": "LP",
                }
            ],
            preserve_existing_state=True,
            deactivate_missing=False,
            mark_new_pending=False,
        )
        self.assertFalse(
            db.get_ratings_map("enso", include_inactive=True)["old-1"][
                "notify_pending"
            ]
        )

        # Later maintenance may discover a genuinely new row before the next
        # monitor poll. Cache it, but do not let that hide the notification.
        db.upsert_format_snapshot(
            "enso",
            "LP",
            [
                {
                    "album_id": "new-1",
                    "score": "91",
                    "artist": "A",
                    "album": "New",
                    "release_format": "LP",
                }
            ],
            preserve_existing_state=True,
            deactivate_missing=False,
            mark_new_pending=True,
        )
        pending = db.get_ratings_map(
            "enso",
            include_inactive=True,
        )["new-1"]
        self.assertTrue(pending["notify_pending"])

        # A monitor-authoritative save after successful Discord delivery clears
        # the pending flag.
        db.upsert_rating(
            "enso",
            {
                "album_id": "new-1",
                "score": "91",
                "artist": "A",
                "album": "New",
                "release_format": "LP",
            },
            record_history=True,
        )
        self.assertFalse(
            db.get_ratings_map("enso", include_inactive=True)["new-1"][
                "notify_pending"
            ]
        )
        db.close()

    def test_detail_change_history_review_like_and_tracks_is_precise(self):
        db = self.make_db(("enso",))
        db.upsert_rating(
            "enso",
            {
                "album_id": "changes-1",
                "score": "88",
                "artist": "Artist",
                "album": "Album",
                "has_review": True,
                "has_track_ratings": True,
            },
        )

        # First complete detail fetch is the baseline, not a fake "change".
        self.assertTrue(
            db.save_rating_detail(
                "enso",
                "changes-1",
                {
                    "review_text": "old review",
                    "has_review": True,
                    "liked": False,
                    "has_track_ratings": True,
                    "track_ratings": [
                        {"number": 1, "title": "One", "score": "80"},
                    ],
                    "detail_incomplete": False,
                },
            )
        )
        self.assertEqual(db.get_change_history("enso"), [])

        # Known mutable details are periodically revisited for edits even when
        # no card flag changed. A far-future cutoff makes this row due now.
        stale_candidates = db.detail_enrichment_candidates(
            "enso",
            10,
            stale_before=10**20,
        )
        self.assertEqual(stale_candidates[0]["album_id"], "changes-1")

        # A broken/interstitial page cannot erase the known baseline.
        self.assertFalse(
            db.save_rating_detail(
                "enso",
                "changes-1",
                {
                    "review_text": None,
                    "has_review": False,
                    "liked": False,
                    "has_track_ratings": False,
                    "track_ratings": [],
                    "detail_incomplete": True,
                },
            )
        )
        preserved = db.get_rating_detail("enso", "changes-1")
        self.assertEqual(preserved["review_text"], "old review")
        self.assertEqual(preserved["track_ratings"][0]["score"], "80")

        # Next trusted snapshot is diffed against the preserved baseline.
        self.assertTrue(
            db.save_rating_detail(
                "enso",
                "changes-1",
                {
                    "review_text": "new review",
                    "has_review": True,
                    "liked": True,
                    "has_track_ratings": True,
                    "track_ratings": [
                        {"number": 1, "title": "One", "score": "90"},
                        {"number": 2, "title": "Two", "score": "70"},
                    ],
                    "detail_incomplete": False,
                },
            )
        )

        events = db.get_change_history("enso", limit=20)
        event_types = {event["event_type"] for event in events}
        self.assertIn("review_edited", event_types)
        self.assertIn("like_added", event_types)
        self.assertIn("track_rating_changed", event_types)
        self.assertIn("track_rating_added", event_types)

        # Removals are also persisted, not represented as silent DELETEs.
        db.save_rating_detail(
            "enso",
            "changes-1",
            {
                "review_text": None,
                "has_review": False,
                "liked": False,
                "has_track_ratings": False,
                "track_ratings": [],
                "detail_incomplete": False,
            },
        )
        event_types = {
            event["event_type"]
            for event in db.get_change_history("enso", limit=50)
        }
        self.assertIn("review_removed", event_types)
        self.assertIn("like_removed", event_types)
        self.assertIn("track_rating_removed", event_types)
        db.close()

    def test_card_transition_before_detail_baseline_is_not_lost(self):
        db = self.make_db(("enso",))
        db.upsert_rating(
            "enso",
            {
                "album_id": "card-before-detail",
                "score": "66",
                "artist": "A",
                "album": "B",
                "has_review": False,
                "has_track_ratings": False,
                "liked": False,
            },
        )
        db.upsert_rating(
            "enso",
            {
                "album_id": "card-before-detail",
                "score": "66",
                "artist": "A",
                "album": "B",
                "has_review": True,
                "has_track_ratings": True,
                "liked": True,
            },
            record_changes=True,
            source="monitor",
        )

        event_types = {
            event["event_type"]
            for event in db.get_change_history("enso", limit=20)
        }
        self.assertIn("review_added", event_types)
        self.assertIn("like_added", event_types)
        self.assertIn("track_ratings_added", event_types)
        db.close()

    def test_card_flag_change_dirties_trusted_detail_and_schedules_recheck(self):
        db = self.make_db(("enso",))
        db.upsert_rating(
            "enso",
            {
                "album_id": "dirty-1",
                "score": "75",
                "artist": "Artist",
                "album": "Clean",
                "has_review": False,
                "has_track_ratings": False,
                "liked": False,
            },
        )
        db.save_rating_detail(
            "enso",
            "dirty-1",
            {
                "has_review": False,
                "review_text": None,
                "has_track_ratings": False,
                "track_ratings": [],
                "liked": False,
                "detail_incomplete": False,
            },
        )

        # A card now says a review exists. The old trusted state stays intact
        # until the detail page confirms it, but the row becomes immediately due.
        db.upsert_rating(
            "enso",
            {
                "album_id": "dirty-1",
                "score": "75",
                "artist": "Artist",
                "album": "Clean",
                "has_review": True,
                "has_track_ratings": False,
                "liked": False,
            },
            record_changes=True,
        )
        rating = db.get_rating("enso", "dirty-1")
        self.assertFalse(rating["has_review"])
        self.assertFalse(rating["detail_complete"])
        candidates = db.detail_enrichment_candidates(
            "enso",
            10,
            stale_before=0,
        )
        self.assertEqual(candidates[0]["album_id"], "dirty-1")
        db.close()

    def test_unified_history_tracks_scores_archive_removals_and_profile_changes(self):
        db = self.make_db(("enso",))
        db.upsert_rating(
            "enso",
            {
                "album_id": "score-1",
                "score": "70",
                "artist": "A",
                "album": "B",
                "release_format": "LP",
            },
        )
        db.upsert_rating(
            "enso",
            {
                "album_id": "score-1",
                "score": "90",
                "artist": "A",
                "album": "B",
                "release_format": "LP",
            },
            record_history=True,
            record_changes=True,
            source="monitor",
        )

        # A complete later LP snapshot owns membership and can persist removal.
        db.upsert_format_snapshot(
            "enso",
            "LP",
            [
                {
                    "album_id": "score-2",
                    "score": "80",
                    "artist": "A",
                    "album": "C",
                    "release_format": "LP",
                }
            ],
            deactivate_missing=True,
            record_history=True,
            record_changes=True,
        )

        db.save_profile(
            "enso",
            {
                "username": "enso",
                "followers_count": "10",
                "favorite_kind": "artists",
                "favorites": [
                    {"type": "artist", "name": "A", "artist": "A", "url": "/a"}
                ],
            },
        )
        db.save_profile(
            "enso",
            {
                "username": "enso",
                "followers_count": "11",
                "favorite_kind": "artists",
                "favorites": [
                    {"type": "artist", "name": "B", "artist": "B", "url": "/b"}
                ],
            },
        )

        events = db.get_change_history("enso", limit=50)
        event_types = {event["event_type"] for event in events}
        self.assertIn("score_changed", event_types)
        self.assertIn("rating_removed", event_types)
        self.assertIn("profile_field_changed", event_types)
        self.assertIn("favorites_changed", event_types)
        self.assertGreaterEqual(db.diagnostics()["counts"]["history"], 4)
        db.close()

    def test_release_cache_requires_monitored_rating(self):
        db = self.make_db(("enso",))
        details = {
            "artist": "A",
            "album": "B",
            "url": "https://example/album/99",
            "user_score": "80",
            "ratings_count": "123",
            "tracklist": [],
        }
        self.assertFalse(db.save_release_details("99", details))

        db.upsert_rating(
            "enso",
            {
                "album_id": "99",
                "score": "90",
                "artist": "A",
                "album": "B",
                "url": "https://example/album/99",
            },
        )
        self.assertTrue(db.save_release_details("99", details))
        self.assertEqual(db.get_release_details("99")["ratings_count"], "123")
        db.close()


    def test_archive_diagnostics_exposes_last_success(self):
        db = self.make_db(("enso",))
        db.mark_format_sync(
            "enso",
            "lp",
            success=True,
            item_count=42,
        )
        stats = db.diagnostics()
        user = stats["users"][0]
        self.assertEqual(user["archive_items"], 42)
        self.assertIsNotNone(user["archive_last_success_at"])
        db.close()

    def test_missing_flagged_track_scores_stay_due_and_are_prioritized(self):
        db = self.make_db(("enso",))
        db.upsert_rating(
            "enso",
            {
                "album_id": "review-pending",
                "score": "70",
                "album": "Review",
                "has_review": True,
            },
        )
        db.upsert_rating(
            "enso",
            {
                "album_id": "tracks-pending",
                "score": "80",
                "album": "Tracks",
                "url": "https://example.test/album/1",
                "has_track_ratings": True,
            },
        )

        # Even an inconsistent legacy row marked complete must be retried when
        # the card says Track Ratings exist but no scored rows were persisted.
        with db._lock, db.connection:
            db.connection.execute(
                """
                UPDATE ratings
                SET detail_complete = 1, detail_synced_at = 9999999999
                WHERE username = ? AND album_id = ?
                """,
                ("enso", "tracks-pending"),
            )

        candidates = db.detail_enrichment_candidates(
            "enso",
            10,
            stale_before=0,
        )
        self.assertEqual(candidates[0]["album_id"], "tracks-pending")
        db.close()

    def test_empty_track_parse_cannot_complete_a_known_track_rating(self):
        db = self.make_db(("enso",))
        db.upsert_rating(
            "enso",
            {
                "album_id": "tracks-1",
                "score": "90",
                "album": "Tracks",
                "has_track_ratings": True,
            },
        )

        self.assertFalse(
            db.save_rating_detail(
                "enso",
                "tracks-1",
                {
                    "has_track_ratings": False,
                    "track_ratings": [
                        {"number": 1, "title": "One", "score": None},
                        {"number": 2, "title": "Two", "score": "NR"},
                    ],
                    "detail_incomplete": False,
                },
            )
        )
        pending = db.get_rating_detail("enso", "tracks-1")
        self.assertTrue(pending["has_track_ratings"])
        self.assertTrue(pending["detail_incomplete"])
        self.assertEqual(pending["track_ratings"], [])

        self.assertTrue(
            db.save_rating_detail(
                "enso",
                "tracks-1",
                {
                    "has_track_ratings": True,
                    "track_ratings": [
                        {"number": 1, "title": "One", "score": "88"},
                        {"number": 2, "title": "Two", "score": None},
                    ],
                    "detail_incomplete": False,
                },
            )
        )
        stored = db.get_rating_detail("enso", "tracks-1")
        self.assertEqual(stored["track_ratings"][0]["score"], "88")
        self.assertFalse(stored["detail_incomplete"])

        # /dbstats says "track scores", so NR tracklist rows do not inflate it.
        stats = db.diagnostics()
        self.assertEqual(stats["counts"]["user_track_ratings"], 1)
        self.assertEqual(stats["users"][0]["track_rating_rows"], 1)
        db.close()


class HTTPClientTests(unittest.TestCase):
    def test_fresh_cache_avoids_duplicate_request(self):
        class FakeResponse:
            status_code = 200
            text = "hello"
            url = "https://example.test/page"
            headers = {}

            def raise_for_status(self):
                return None

        client = ResilientHTTPClient()
        client._gate.min_interval = 0
        calls = []

        def fake_get(url, timeout):
            calls.append((url, timeout))
            return FakeResponse()

        client.session.get = fake_get
        first = client.get("https://example.test/page")
        second = client.get("https://example.test/page")

        self.assertEqual(first.text, "hello")
        self.assertTrue(second.from_cache)
        self.assertEqual(len(calls), 1)



class RatingsArchiveTests(unittest.TestCase):
    def test_unlimited_route_reads_until_first_empty_page(self):
        original_fetch = aoty.fetch_page
        original_parse = aoty._parse_ratings_soup
        fetched = []

        try:
            def fake_fetch(url, expected_url=None):
                fetched.append(url)
                page = url.rstrip("/").split("/")[-1]
                if not page.isdigit():
                    page = "1"
                return f"<html><body>{page}</body></html>"

            def fake_parse(soup, forced_format=None):
                page = int(soup.get_text(strip=True))
                if page >= 4:
                    return []
                return [
                    {
                        "album_id": f"album-{page}",
                        "score": "80",
                        "release_format": forced_format,
                    }
                ]

            aoty.fetch_page = fake_fetch
            aoty._parse_ratings_soup = fake_parse

            items = aoty._get_ratings_from_route(
                "enso",
                slug="lp",
                limit=None,
                forced_format="LP",
                max_pages=10,
            )

            self.assertEqual(
                [item["album_id"] for item in items],
                ["album-1", "album-2", "album-3"],
            )
            self.assertEqual(len(fetched), 4)
        finally:
            aoty.fetch_page = original_fetch
            aoty._parse_ratings_soup = original_parse

    def test_unlimited_route_refuses_to_mark_endless_pagination_complete(self):
        original_fetch = aoty.fetch_page
        original_parse = aoty._parse_ratings_soup
        counter = {"page": 0}

        try:
            def fake_fetch(url, expected_url=None):
                counter["page"] += 1
                return "<html></html>"

            def fake_parse(soup, forced_format=None):
                page = counter["page"]
                return [{"album_id": f"album-{page}", "score": "80"}]

            aoty.fetch_page = fake_fetch
            aoty._parse_ratings_soup = fake_parse

            with self.assertRaises(aoty.AOTYArchiveIncomplete):
                aoty._get_ratings_from_route(
                    "enso",
                    slug="lp",
                    limit=None,
                    max_pages=3,
                )
        finally:
            aoty.fetch_page = original_fetch
            aoty._parse_ratings_soup = original_parse


class ParserTests(unittest.TestCase):
    def test_album_rating_count_ignores_unrelated_counts(self):
        soup = BeautifulSoup(
            """
            <div>User Score</div><div>61</div>
            <div>Based on 574 ratings</div>
            <div>2025 Ratings: #3,076</div>
            <h2>Details</h2>
            <div>Based on 206 ratings</div>
            """,
            "html.parser",
        )
        self.assertEqual(aoty._extract_ratings_count(soup), "574")

    def test_numberless_track_rating_is_preserved(self):
        soup = BeautifulSoup(
            """
            <h3>Track Ratings</h3>
            <div><span>DEMONSTRATION</span><span>88</span></div>
            <h3>Play This On</h3>
            """,
            "html.parser",
        )
        ratings = aoty._extract_user_track_ratings(soup)
        self.assertTrue(any(item.get("score") == "88" for item in ratings))


if __name__ == "__main__":
    unittest.main()
