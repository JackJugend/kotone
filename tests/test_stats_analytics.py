"""Offline regressions for config-only SQLite statistics and PNG cards."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="kotone-stats-runtime-"))

from database import Database  # noqa: E402
from must_hear import must_hear_album  # noqa: E402
from shared import username_autocomplete  # noqa: E402
from stats_engine import (  # noqa: E402
    compare,
    rating_distribution,
    summarize,
    wrapped,
)


DISCORD_IMPORT_ERROR = None
try:
    import discord  # noqa: E402
    from commands.analytics import (  # noqa: E402
        RATING_DISTRIBUTION_FORMATS,
        RatingDistributionView,
        setup_analytics_commands,
    )
except ModuleNotFoundError as exc:  # pragma: no cover - minimal local runtime
    DISCORD_IMPORT_ERROR = exc


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
    def test_score_distribution_matches_aoty_ranges(self):
        data = summarize("enso", [row(str(score), str(score)) for score in range(101)])
        self.assertEqual(
            [label for label, _ in data["score_buckets"]],
            [
                "100", "90–99", "80–89", "70–79", "60–69", "50–59",
                "40–49", "30–39", "20–29", "10–19", "0–9",
            ],
        )
        self.assertEqual(data["score_buckets"][0], ("100", 1))
        self.assertEqual(data["score_buckets"][1], ("90–99", 10))

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

    def test_summary_keeps_ten_most_common_genres_for_the_chart(self):
        rows = [
            row(str(index), "80", genres=[f"Genre {index:02d}"])
            for index in range(12)
        ]
        data = summarize("enso", rows)
        self.assertEqual(len(data["top_genres"]), 10)

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
        self.assertEqual(data["median_a"], 95)
        self.assertEqual(data["median_b"], 40)
        self.assertEqual(data["ahead_a"][0]["album_id"], "shared")

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

    def test_distribution_filters_every_format_year_genre_and_score(self):
        lp = row("lp", "90", genres=["Art Pop"])
        single = row("single", "80", genres=["Dance-Pop"])
        single["release_format"] = "Single"
        track = {
            "score": "95",
            "release_year": "2024",
            "genres": ["Art Pop"],
            "_track_score": True,
        }

        everything = rating_distribution(
            "enso", [lp, single], [track], "all", category_label="Wszystko"
        )
        self.assertEqual(everything["ratings"], 3)
        self.assertEqual(everything["best_examples"][0]["album_id"], "lp")
        self.assertEqual(everything["worst_examples"][0]["album_id"], "single")

        singles = rating_distribution(
            "enso", [lp, single], [track], "single", category_label="Single"
        )
        self.assertEqual(singles["ratings"], 1)
        self.assertEqual(singles["average"], 80)

        tracks = rating_distribution(
            "enso",
            [lp, single],
            [track],
            "tracks",
            category_label="Oceny utworów",
            year=2024,
            genre="Art Pop",
            score_min=91,
            score_max=100,
        )
        self.assertEqual(tracks["ratings"], 1)
        self.assertEqual(tracks["average"], 95)

    def test_must_hear_rules_use_their_independent_aoty_thresholds(self):
        self.assertTrue(must_hear_album("84", "2,204", "74", "18"))
        self.assertFalse(must_hear_album("80", "2,204", "74", "18"))
        self.assertFalse(must_hear_album("84", "499", "74", "18"))
        self.assertTrue(must_hear_album("84", "2,204", "80", "18"))
        self.assertTrue(must_hear_album("84", "2,204", "74", "14"))
        self.assertTrue(must_hear_album("60", "12", "84", "15"))
        self.assertFalse(must_hear_album("60", "12", "84", "14"))

    def test_official_must_hear_marker_overrides_legacy_score_heuristic(self):
        self.assertTrue(
            must_hear_album(
                "79.5", "1,965", "70.1", "2", official=True
            )
        )
        self.assertFalse(
            must_hear_album(
                "84", "2,204", "74", "18", official=False
            )
        )


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
                "cover": "https://cdn.albumoftheyear.org/example.jpg",
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
                "user_score": "84",
                "ratings_count": "2,204",
                "critic_score": "74",
                "critic_reviews_count": "18",
            },
        )

        rows = self.db.get_analytics_rows("enso")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["score"], "88")
        self.assertEqual(rows[0]["genres"], ["Dream Pop"])
        self.assertTrue(rows[0]["has_review"])
        self.assertTrue(rows[0]["liked"])
        self.assertEqual(rows[0]["critic_score"], "74")
        self.assertEqual(rows[0]["critic_reviews_count"], "18")
        self.assertEqual(
            rows[0]["cover"],
            "https://cdn.albumoftheyear.org/example.jpg",
        )
        self.assertEqual(self.db.get_analytics_rows("outsider"), [])

    def test_track_distribution_rows_include_parent_release_metadata(self):
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "track-release",
                "score": "91",
                "artist": "Artist",
                "album": "Album",
                "release_format": "EP",
                "sort_timestamp": 1735689600,
            },
        )
        self.db.save_release_details(
            "track-release",
            {
                "artist": "Artist",
                "album": "Album",
                "genres": ["Ambient"],
                "release_date": "January 1, 2024",
                "year": "2024",
                "album_format": "EP",
            },
        )
        self.db.save_rating_detail(
            "enso",
            "track-release",
            {
                "has_track_ratings": True,
                "track_ratings": [
                    {"track_number": 1, "title": "One", "score": "96"},
                    {"track_number": 2, "title": "Two", "score": "88"},
                ],
            },
        )

        rows = self.db.get_analytics_track_rows("enso")
        self.assertEqual([item["score"] for item in rows], ["96", "88"])
        self.assertEqual(rows[0]["genres"], ["Ambient"])
        self.assertEqual(rows[0]["release_year"], "2024")
        self.assertEqual(self.db.get_analytics_track_rows("outsider"), [])


class CoverCacheTests(unittest.TestCase):
    def test_cover_cache_accepts_only_known_https_image_hosts(self):
        from stats_cover_cache import _safe_cover_url

        self.assertEqual(
            _safe_cover_url("https://cdn.albumoftheyear.org/cover.jpg"),
            "https://cdn.albumoftheyear.org/cover.jpg",
        )
        self.assertEqual(
            _safe_cover_url("//coverartarchive.org/release/cover.jpg"),
            "https://coverartarchive.org/release/cover.jpg",
        )
        self.assertIsNone(_safe_cover_url("http://cdn.albumoftheyear.org/x.jpg"))
        self.assertIsNone(_safe_cover_url("https://example.com/x.jpg"))

    def test_marked_cover_url_is_stable_and_badges_use_their_three_colours(self):
        try:
            from PIL import Image
            from cover_badges import (
                MUST_HEAR_BLUE,
                MUST_HEAR_ORANGE,
                MUST_HEAR_PURPLE,
                add_must_hear_badge,
            )
            from must_hear import marked_cover_url
        except ModuleNotFoundError as exc:
            self.skipTest(str(exc))

        with patch.dict(
            os.environ,
            {"RAILWAY_PUBLIC_DOMAIN": "kotone.example"},
        ):
            url = marked_cover_url("42", "https://cdn.albumoftheyear.org/x.jpg")
        self.assertTrue(url.startswith("https://kotone.example/must-hear-cover/42/"))
        image = add_must_hear_badge(Image.new("RGB", (200, 200), "white"))
        self.assertEqual(image.getpixel((198, 1)), MUST_HEAR_ORANGE)
        critics = add_must_hear_badge(
            Image.new("RGB", (200, 200), "white"), kind="critics"
        )
        both = add_must_hear_badge(
            Image.new("RGB", (200, 200), "white"), kind="both"
        )
        self.assertEqual(critics.getpixel((198, 1)), MUST_HEAR_BLUE)
        self.assertEqual(both.getpixel((198, 1)), MUST_HEAR_PURPLE)


@unittest.skipIf(DISCORD_IMPORT_ERROR is not None, str(DISCORD_IMPORT_ERROR))
class DistributionViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_username_choices_are_visible_before_typing(self):
        choices = await username_autocomplete(None, "")
        self.assertEqual(
            [choice.value for choice in choices],
            ["enso", "kulkien"],
        )

    async def test_setup_registers_chart_first_distribution_command(self):
        client = discord.Client(intents=discord.Intents.none())
        tree = discord.app_commands.CommandTree(client)
        setup_analytics_commands(tree)
        try:
            self.assertEqual(
                {command.name for command in tree.get_commands()},
                {"stats", "compare", "wrapped", "ratingdistribution"},
            )
        finally:
            await client.close()

    async def test_rating_distribution_menu_contains_every_bot_format(self):
        data = rating_distribution("enso", [], [], "all", category_label="Wszystko")
        distributions = {
            key: {**data, "category": key, "label": label}
            for key, label in RATING_DISTRIBUTION_FORMATS
        }
        view = RatingDistributionView(
            distributions=distributions,
            avatar_items=[],
        )
        try:
            self.assertEqual(len(view.selector.options), 21)
            self.assertEqual(view.selector.options[0].value, "all")
            self.assertEqual(view.selector.options[1].value, "tracks")
            self.assertTrue(view.selector.options[0].default)
        finally:
            view.stop()


class GraphicTests(unittest.TestCase):
    def test_unicode_fonts_are_bundled_with_the_project(self):
        for filename in (
            "NotoSans-Regular.ttf",
            "NotoSans-Bold.ttf",
            "NotoSans-LICENSE.txt",
        ):
            with self.subTest(filename=filename):
                self.assertTrue((ROOT / "assets" / filename).is_file())

    def test_all_cards_are_valid_pngs(self):
        try:
            from stats_graphics import (
                render_compare,
                render_rating_distribution,
                render_stats,
                render_wrapped,
            )
        except ModuleNotFoundError as exc:
            self.skipTest(str(exc))

        rows = [row("1", "90", genres=["Pop"])]
        stats = summarize("enso", rows)
        comparison = compare("enso", rows, "kulkien", [row("1", "80")])
        yearly = wrapped("enso", rows, 2025)
        distribution = rating_distribution(
            "enso", rows, [], "all", category_label="Wszystko"
        )
        distribution["filter_text"] = "Wszystko · Wszystkie lata"
        for renderer, payload in (
            (render_stats, stats),
            (render_compare, comparison),
            (render_wrapped, yearly),
            (render_rating_distribution, distribution),
        ):
            with self.subTest(renderer=renderer.__name__):
                image = renderer(payload)
                self.assertEqual(image.read(8), b"\x89PNG\r\n\x1a\n")
