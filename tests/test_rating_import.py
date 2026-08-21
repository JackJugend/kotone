"""Offline tests for the official AOTY CSV import path."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kotone-import-runtime-")

from database import Database  # noqa: E402
from rating_import import (  # noqa: E402
    RatingImportError,
    parse_aoty_ratings_csv,
    unmatched_report_csv,
)

DISCORD_IMPORT_ERROR = None
try:
    import discord  # noqa: E402
    from commands.rating_import import (  # noqa: E402
        IMPORT_USERS_BY_DISCORD_ID,
        setup_rating_import_command,
    )
    from commands.manual import setup_manual_command  # noqa: E402
    from monitor import MONITOR_STATE_VERSION, RatingMonitor  # noqa: E402
except Exception as exc:  # pragma: no cover - dependency-limited local runs
    DISCORD_IMPORT_ERROR = exc


def csv_payload(*rows: str) -> bytes:
    header = 'Artist,Album,Year,Type,Rating,"Date Rated"\n'
    return (header + "\n".join(rows) + "\n").encode("utf-8")


class RatingImportParserTests(unittest.TestCase):
    def test_official_utf8_export_is_parsed(self):
        parsed = parse_aoty_ratings_csv(
            csv_payload(
                '"tripleS & moon","Dream Dress",2026,Single,80,2026-08-16',
                'Akute,"Dzievački i kosmas",2011,LP,85,2026-08-15',
            )
        )
        self.assertEqual(len(parsed["rows"]), 2)
        self.assertEqual(parsed["rows"][0]["score"], "80")
        self.assertEqual(parsed["rows"][1]["year"], 2011)
        self.assertGreater(parsed["rows"][0]["sort_timestamp"], 0)

    def test_known_nct_lp_and_single_receive_distinct_verified_ids(self):
        parsed = parse_aoty_ratings_csv(
            csv_payload(
                "NCT,Golden Age,2023,LP,73,2023-08-28",
                "NCT,Golden Age,2023,Single,80,2023-08-23",
            )
        )
        self.assertEqual(parsed["rows"][0]["album_id_hint"], "722118")
        self.assertEqual(parsed["rows"][1]["album_id_hint"], "732107")

    def test_known_world_wild_women_has_verified_id_and_release_metadata(self):
        parsed = parse_aoty_ratings_csv(
            csv_payload(
                'tripleS,"World Wild Women",2026,Single,50,2026-08-17'
            )
        )
        row = parsed["rows"][0]
        self.assertEqual(row["album_id_hint"], "1981558")
        self.assertEqual(row["release_details_hint"]["user_score"], "71")
        self.assertEqual(
            row["release_details_hint"]["genres"],
            ["K-Pop", "Contemporary R&B"],
        )

    def test_invalid_rows_are_reported_and_duplicate_identities_are_kept(self):
        parsed = parse_aoty_ratings_csv(
            csv_payload(
                "Artist,Album,2020,LP,90,2026-08-16",
                "Artist,Album,2020,LP,90,2026-08-16",
                "Broken,Score,2020,LP,999,2026-08-16",
            )
        )
        self.assertEqual(len(parsed["rows"]), 2)
        self.assertEqual(parsed["duplicates"], 1)
        self.assertEqual(len(parsed["rejected"]), 1)

    def test_wrong_header_is_rejected(self):
        with self.assertRaises(RatingImportError):
            parse_aoty_ratings_csv(b"Artist,Album,Rating\nA,B,90\n")

    def test_report_neutralizes_spreadsheet_formulas(self):
        content = unmatched_report_csv(
            [{"artist": "=cmd", "album": "+SUM(1,2)", "reason": "missing"}]
        ).decode("utf-8-sig")
        self.assertIn("'=cmd", content)
        self.assertIn("'+SUM(1,2)", content)


class RatingImportDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kotone-import-test-"))
        self.db = Database(
            str(self.tmp / "kotone.sqlite3"),
            monitored_users=("enso", "kulkien"),
            backup_path=str(self.tmp / "kotone.backup.sqlite3"),
        )

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _record(self, artist="Akute", album="Dzievački i kosmas", score="85"):
        return {
            "source_row": 2,
            "artist": artist,
            "album": album,
            "year": 2011,
            "release_format": "LP",
            "score": score,
            "date": "15.08.2026",
            "sort_timestamp": 1_787_000_000.0,
        }

    def test_import_updates_only_rating_fields_and_preserves_rich_detail(self):
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "10",
                "artist": "Akute",
                "album": "Dzievački i kosmas",
                "score": "80",
                "date": "2025-01-01",
                "release_format": "LP",
                "has_review": True,
                "has_track_ratings": True,
                "liked": True,
            },
        )
        self.db.save_rating_detail(
            "enso",
            "10",
            {
                "has_review": True,
                "review_text": "review",
                "has_track_ratings": True,
                "track_ratings": [{"number": 1, "title": "Track", "score": "99"}],
                "liked": True,
                "detail_incomplete": False,
            },
        )

        result = self.db.import_official_ratings("enso", [self._record()])
        detail = self.db.get_rating_detail("enso", "10")
        self.assertEqual(result["updated"], 1)
        self.assertEqual(detail["score"], "85")
        self.assertEqual(detail["review_text"], "review")
        self.assertTrue(detail["liked"])
        self.assertEqual(detail["track_ratings"][0]["score"], "99")

    def test_unique_config_scoped_album_id_can_add_other_config_user(self):
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "10",
                "artist": "Akute",
                "album": "Dzievački i kosmas",
                "score": "80",
                "release_format": "LP",
            },
        )
        result = self.db.import_official_ratings("kulkien", [self._record()])
        self.assertEqual(result["added"], 1)
        self.assertEqual(self.db.get_rating("kulkien", "10")["score"], "85")

    def test_verified_hint_adds_rating_and_aoty_release_cache(self):
        parsed = parse_aoty_ratings_csv(
            csv_payload(
                'tripleS,"World Wild Women",2026,Single,50,2026-08-17'
            )
        )
        self.db.mark_notification_delivered(
            "enso",
            delivered_at=parsed["rows"][0]["sort_timestamp"] - 1,
        )
        result = self.db.import_official_ratings("enso", parsed["rows"])
        rating = self.db.get_rating("enso", "1981558")
        release = self.db.get_release_details("1981558")
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["queued_notifications"], 1)
        self.assertEqual(rating["score"], "50")
        self.assertTrue(rating["notify_pending"])
        self.assertEqual(rating["cover"], parsed["rows"][0]["release_details_hint"]["cover"])
        self.assertEqual(release["user_score"], "71")
        self.assertEqual(release["ratings_count"], "20")
        self.assertEqual(release["label"], "Modhaus")
        self.assertEqual(release["secondary_genres"], ["New Jack Swing"])

    def test_verified_nct_hints_create_two_distinct_releases(self):
        parsed = parse_aoty_ratings_csv(
            csv_payload(
                "NCT,Golden Age,2023,LP,73,2023-08-28",
                "NCT,Golden Age,2023,Single,80,2023-08-23",
            )
        )
        result = self.db.import_official_ratings("enso", parsed["rows"])
        self.assertEqual(result["added"], 2)
        self.assertEqual(result["unmatched"], [])
        self.assertEqual(self.db.get_rating("enso", "722118")["score"], "73")
        self.assertEqual(self.db.get_rating("enso", "732107")["score"], "80")
        self.assertFalse(self.db.get_rating("enso", "722118")["notify_pending"])
        self.assertFalse(self.db.get_rating("enso", "732107")["notify_pending"])

    def test_import_queues_only_new_rows_from_last_seven_days(self):
        cutoff = time.time() - 7 * 24 * 60 * 60
        older = {
            **self._record(artist="Older", album="Older", score="70"),
            "album_id_hint": "older-id",
            "album_url_hint": "https://example.invalid/older",
            "sort_timestamp": cutoff - 1,
        }
        newer = {
            **self._record(artist="Newer", album="Newer", score="90"),
            "album_id_hint": "newer-id",
            "album_url_hint": "https://example.invalid/newer",
            "sort_timestamp": time.time(),
        }
        result = self.db.import_official_ratings("enso", [older, newer])
        self.assertEqual(result["added"], 2)
        self.assertEqual(result["queued_notifications"], 1)
        self.assertFalse(
            self.db.get_rating("enso", "older-id")["notify_pending"]
        )
        self.assertTrue(
            self.db.get_rating("enso", "newer-id")["notify_pending"]
        )

    def test_recent_cached_row_is_not_requeued_by_import(self):
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "cached-id",
                "artist": "Cached Artist",
                "album": "Cached Album",
                "score": "90",
                "release_format": "Single",
            },
        )
        record = {
            **self._record(
                artist="Cached Artist",
                album="Cached Album",
                score="90",
            ),
            "release_format": "Single",
            "sort_timestamp": time.time(),
        }
        result = self.db.import_official_ratings("enso", [record])
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["queued_notifications"], 0)
        self.assertFalse(self.db.get_rating("enso", "cached-id")["notify_pending"])

    def test_changed_csv_row_keeps_old_score_for_offline_monitor_delivery(self):
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "pending-change",
                "artist": "Akute",
                "album": "Dzievački i kosmas",
                "score": "70",
                "release_format": "LP",
            },
        )
        record = {
            **self._record(score="85"),
            "album_id_hint": "pending-change",
            "sort_timestamp": time.time(),
        }
        result = self.db.import_official_ratings("enso", [record])
        self.assertEqual(result["queued_notifications"], 0)
        pending = self.db.get_pending_notifications("enso")
        self.assertEqual(pending, [])

    def test_manual_review_and_like_preserve_tracks_and_become_due(self):
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "10",
                "artist": "Akute",
                "album": "Dzievački i kosmas",
                "score": "85",
                "release_format": "LP",
            },
        )
        self.db.save_rating_detail(
            "enso",
            "10",
            {
                "has_review": False,
                "has_track_ratings": True,
                "track_ratings": [
                    {"number": 1, "title": "Track", "score": "99"}
                ],
                "liked": False,
                "detail_incomplete": False,
            },
        )

        review_result = self.db.manual_update_rating_detail(
            "enso",
            "10",
            "review_set",
            review_text="Ręczna recenzja",
        )
        like_result = self.db.manual_update_rating_detail(
            "enso",
            "10",
            "like_on",
        )
        detail = self.db.get_rating_detail("enso", "10")
        due_ids = {
            row["album_id"]
            for row in self.db.detail_enrichment_candidates("enso", 10)
        }

        self.assertTrue(review_result["changed"])
        self.assertTrue(like_result["changed"])
        self.assertEqual(detail["review_text"], "Ręczna recenzja")
        self.assertTrue(detail["liked"])
        self.assertEqual(detail["track_ratings"][0]["score"], "99")
        self.assertIn("10", due_ids)

    def test_complete_aoty_detail_overwrites_manual_values(self):
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "10",
                "artist": "Akute",
                "album": "Dzievački i kosmas",
                "score": "85",
            },
        )
        self.db.manual_update_rating_detail(
            "enso", "10", "review_set", review_text="Tymczasowa"
        )
        self.db.manual_update_rating_detail("enso", "10", "like_on")

        saved = self.db.save_rating_detail(
            "enso",
            "10",
            {
                "has_review": False,
                "review_text": None,
                "has_track_ratings": False,
                "track_ratings": [],
                "liked": False,
                "detail_incomplete": False,
            },
            source="aoty_detail_sync",
        )
        detail = self.db.get_rating_detail("enso", "10")
        self.assertTrue(saved)
        self.assertFalse(detail["has_review"])
        self.assertIsNone(detail["review_text"])
        self.assertFalse(detail["liked"])

    def test_same_title_releases_are_disambiguated_by_existing_score(self):
        for album_id, score in (("single-a", "87"), ("single-b", "100")):
            self.db.upsert_rating(
                "enso",
                {
                    "album_id": album_id,
                    "artist": "Cocteau Twins",
                    "album": "Violaine",
                    "score": score,
                    "date": "15.08.2026",
                    "sort_timestamp": 1_787_000_000.0,
                    "release_format": "Single",
                },
            )
        records = [
            {
                **self._record(
                    artist="Cocteau Twins",
                    album="Violaine",
                    score=score,
                ),
                "year": 1996,
                "release_format": "Single",
            }
            for score in ("87", "100")
        ]
        result = self.db.import_official_ratings("enso", records)
        self.assertEqual(result["unchanged"], 2)
        self.assertEqual(result["unmatched"], [])

    def test_unknown_album_is_reported_without_inventing_an_id(self):
        self.db.upsert_rating(
            "enso",
            {"album_id": "10", "artist": "Known", "album": "Known", "score": "80"},
        )
        result = self.db.import_official_ratings(
            "enso",
            [self._record(artist="Unknown", album="Missing")],
        )
        self.assertEqual(len(result["unmatched"]), 1)
        self.assertEqual(self.db.summary()["ratings"], 1)

    def test_explicit_manual_release_is_a_safe_csv_import_candidate(self):
        """An operator-provided AOTY ID may resolve one previously unknown row."""

        self.db.manual_update_release_details(
            "manual-spinning-plums",
            {
                "artist": "Spinning Plums",
                "album": "Spinning Plums",
                "album_format": "EP",
                "year": "2024",
                "_section_complete": {"format": True, "release_date": True},
            },
        )
        record = {
            **self._record(artist="Spinning Plums", album="Spinning Plums", score="57"),
            "year": 2024,
            "release_format": "EP",
        }

        result = self.db.import_official_ratings("enso", [record])

        self.assertEqual(result["unmatched"], [])
        self.assertEqual(result["added"], 1)
        self.assertEqual(
            self.db.get_rating("enso", "manual-spinning-plums")["score"],
            "57",
        )

    def test_probably_swapped_export_is_rejected_before_changes(self):
        for index in range(10):
            self.db.upsert_rating(
                "enso",
                {
                    "album_id": str(index),
                    "artist": f"Enso {index}",
                    "album": f"Album {index}",
                    "score": "80",
                },
            )
        wrong = [
            self._record(artist=f"Other {index}", album=f"Release {index}")
            for index in range(10)
        ]
        with self.assertRaises(ValueError):
            self.db.import_official_ratings("enso", wrong)
        self.assertEqual(self.db.get_rating("enso", "0")["score"], "80")


@unittest.skipIf(DISCORD_IMPORT_ERROR is not None, str(DISCORD_IMPORT_ERROR))
class RatingImportCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_is_registered_and_ids_map_to_own_profiles(self):
        self.assertEqual(IMPORT_USERS_BY_DISCORD_ID[805601151366070292], "enso")
        self.assertEqual(IMPORT_USERS_BY_DISCORD_ID[463642066401099786], "kulkien")
        client = discord.Client(intents=discord.Intents.none())
        tree = discord.app_commands.CommandTree(client)
        try:
            setup_rating_import_command(tree)
            setup_manual_command(tree)
            command_names = {command.name for command in tree.get_commands()}
            self.assertIn("import", command_names)
            self.assertIn("manual", command_names)
        finally:
            await client.close()


@unittest.skipIf(DISCORD_IMPORT_ERROR is not None, str(DISCORD_IMPORT_ERROR))
class MonitorNotificationMarkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_and_changed_delivery_both_advance_import_cutoff(self):
        fake_db = MagicMock()
        fake_db.canonical_username.return_value = "enso"
        fake_db.sync_timestamps.return_value = {
            "ratings_synced_at": time.time(),
            "full_ratings_synced_at": time.time(),
        }
        fake_db.get_monitor_version.return_value = MONITOR_STATE_VERSION
        fake_db.get_ratings_map.return_value = {
            "changed": {
                "album_id": "changed",
                "score": "70",
                "active": True,
                "notify_pending": False,
            }
        }
        fake_db.get_avatar.return_value = None
        ratings = [
            {
                "album_id": "new",
                "artist": "Artist",
                "album": "New",
                "score": "90",
            },
            {
                "album_id": "changed",
                "artist": "Artist",
                "album": "Changed",
                "score": "80",
            },
        ]
        fake_data = MagicMock()
        fake_data.fetch_ratings_live = AsyncMock(return_value=ratings)

        instance = RatingMonitor(MagicMock())
        instance._db_only_enabled = MagicMock(return_value=False)
        instance._challenge_remaining_seconds = MagicMock(return_value=0.0)
        instance._refresh_profile_if_due = AsyncMock()
        instance.send_new_rating = AsyncMock(return_value=True)
        instance.send_changed_rating = AsyncMock(return_value=True)
        instance._sleep = AsyncMock()

        with patch("monitor.DB", fake_db), patch("monitor.DATA", fake_data):
            result = await instance.check_user("enso", allow_full=False)

        self.assertEqual(result["sent_new"], 1)
        self.assertEqual(result["sent_changed"], 1)
        self.assertEqual(fake_db.mark_notification_delivered.call_count, 2)


if __name__ == "__main__":
    unittest.main()
