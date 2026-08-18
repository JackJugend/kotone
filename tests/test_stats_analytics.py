"""Offline regressions for config-only SQLite statistics and PNG cards."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="kotone-stats-runtime-"))

from database import Database  # noqa: E402
from stats_engine import compare, summarize, wrapped  # noqa: E402


def row(
    album_id: str,
    score: str,
    *,
    artist: str = "Artist",
    album: str = "Album",
    genres=None,
    timestamp: float = 1735689600,
    review: bool = False,
    liked: bool = False,
    tracks: int = 0,
):
    return {
        "album_id": album_id,
        "score": score,
        "artist": artist,
        "album": album,
        "genres": list(genres or []),
        "release_format": "LP",
        "release_year": "2024",
        "sort_timestamp": timestamp,
        "has_review": review,
        "liked": liked,
        "has_track_ratings": tracks > 0,
        "track_score_count": tracks,
    }


class StatsEngineTests(unittest.TestCase):
    def test_summary_uses_numeric_saved_scores_and_flags(self):
        data = summarize(
            "enso",
            [
                row("1", "100", genres=["Art Pop"], review=True, tracks=2),
                row("2", "80", genres=["Art Pop", "Ambient"], liked=True),
                row("3", "NR", genres=["Rock"]),
            ],
        )
        self.assertEqual(data["ratings"], 2)
        self.assertEqual(data["average"], 90)
        self.assertEqual(data["median"], 90)
        self.assertEqual(data["reviews"], 1)
        self.assertEqual(data["likes"], 1)
        self.assertEqual(data["track_scores"], 2)
        self.assertEqual(data["top_genres"][0], ("Art Pop", 2))

    def test_compare_uses_only_shared_album_ids(self):
        data = compare(
            "enso",
            [row("shared", "90"), row("solo-a", "100")],
            "kulkien",
            [row("shared", "70"), row("solo-b", "10")],
        )
        self.assertEqual(data["common_count"], 1)
        self.assertEqual(data["mean_gap"], 20)
        self.assertEqual(data["agreement"], 80)
        self.assertEqual(data["disagreements"][0]["album_id"], "shared")

    def test_wrapped_filters_by_saved_rating_year(self):
        year_2025 = 1735689600  # 2025-01-01 UTC
        year_2024 = 1704067200  # 2024-01-01 UTC
        data = wrapped(
            "enso",
            [
                row("2025", "91", timestamp=year_2025),
                row("2024", "50", timestamp=year_2024),
            ],
            2025,
        )
        self.assertEqual(data["ratings"], 1)
        self.assertEqual(data["average"], 91)
        self.assertEqual(data["months"][0], (1, 1))


class AnalyticsDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kotone-stats-db-"))
        self.db = Database(
            str(self.tmp / "kotone.sqlite3"),
            monitored_users=("enso", "kulkien"),
        )

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_read_model_is_config_scoped_and_joins_release_metadata(self):
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "42",
                "score": "88",
                "artist": "Test Artist",
                "album": "Test Album",
                "release_format": "LP",
                "has_review": True,
                "liked": True,
                "sort_timestamp": 1735689600,
            },
        )
        self.db.save_release_details(
            "42",
            {
                "artist": "Test Artist",
                "album": "Test Album",
                "genres": ["Dream Pop"],
                "release_date": "January 1, 2024",
                "year": "2024",
                "album_format": "LP",
            },
        )

        rows = self.db.get_analytics_rows("enso")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["score"], "88")
        self.assertEqual(rows[0]["genres"], ["Dream Pop"])
        self.assertTrue(rows[0]["has_review"])
        self.assertTrue(rows[0]["liked"])
        self.assertEqual(self.db.get_analytics_rows("outsider"), [])


class GraphicTests(unittest.TestCase):
    def test_all_cards_are_valid_pngs(self):
        try:
            from stats_graphics import render_compare, render_stats, render_wrapped
        except ModuleNotFoundError as exc:
            self.skipTest(str(exc))

        rows = [row("1", "90", genres=["Pop"])]
        stats = summarize("enso", rows)
        comparison = compare("enso", rows, "kulkien", [row("1", "80")])
        yearly = wrapped("enso", rows, 2025)
        for renderer, payload in (
            (render_stats, stats),
            (render_compare, comparison),
            (render_wrapped, yearly),
        ):
            with self.subTest(renderer=renderer.__name__):
                image = renderer(payload)
                self.assertEqual(image.read(8), b"\x89PNG\r\n\x1a\n")
