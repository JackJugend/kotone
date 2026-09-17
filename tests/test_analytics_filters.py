"""Common filter coverage for every analytics command that returns a graphic."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="kotone-filter-runtime-"))

import discord

from commands.analytics import setup_analytics_commands
from stats_engine import filter_rating_rows


COMMON_FILTERS = {
    "release_year", "genre", "format", "score_min", "score_max",
    "reviewed", "liked", "has_tracks", "artist",
}


class AnalyticsFilterTests(unittest.TestCase):
    def test_common_filters_combine_without_mutating_source_rows(self):
        matching = {
            "album_id": "match", "score": "84.5", "release_year": 2024,
            "genres": ["Art Pop", "Electronic"], "release_format": "LP",
            "has_review": True, "liked": False, "has_track_ratings": True,
            "artist": "Björk & Friends",
        }
        rows = [
            matching,
            {**matching, "album_id": "year", "release_year": 2023},
            {**matching, "album_id": "genre", "genres": ["Rock"]},
            {**matching, "album_id": "format", "release_format": "EP"},
            {**matching, "album_id": "low", "score": "69"},
            {**matching, "album_id": "high", "score": "96"},
            {**matching, "album_id": "review", "has_review": False},
            {**matching, "album_id": "like", "liked": True},
            {**matching, "album_id": "tracks", "has_track_ratings": False},
            {**matching, "album_id": "artist", "artist": "Someone Else"},
        ]
        selected = filter_rating_rows(
            rows,
            release_year=2024,
            genre="art pop",
            release_format="lp",
            score_min=70,
            score_max=90,
            reviewed=True,
            liked=False,
            has_tracks=True,
            artist="BJÖRK",
        )
        self.assertEqual(selected, [matching])
        self.assertEqual(len(rows), 10)

    def test_all_graphic_commands_expose_many_relevant_filters(self):
        client = discord.Client(intents=discord.Intents.none())
        tree = discord.app_commands.CommandTree(client)
        setup_analytics_commands(tree)
        try:
            for name in ("stats", "chart", "compare", "wrapped"):
                with self.subTest(command=name):
                    parameters = {item.name: item for item in tree.get_command(name).parameters}
                    self.assertTrue(COMMON_FILTERS <= parameters.keys())
                    self.assertEqual(len(parameters["format"].choices), 19)
                    self.assertTrue(parameters["genre"].autocomplete)

            distribution = {
                item.name: item for item in tree.get_command("ratingdistribution").parameters
            }
            self.assertTrue(
                {"year", "genre", "score_min", "score_max", "reviewed", "liked",
                 "has_tracks", "artist"} <= distribution.keys()
            )
        finally:
            asyncio.run(client.close())


if __name__ == "__main__":
    unittest.main()
