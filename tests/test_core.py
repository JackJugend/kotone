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
        db.close()

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
