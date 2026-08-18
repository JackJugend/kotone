"""Offline regression coverage for the SQLite-first /artist view."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kotone-artist-runtime-")

from database import Database  # noqa: E402
from display_utils import display_genres, display_release_date  # noqa: E402
from lastfm import fetch_artist_image  # noqa: E402
from shared import build_release_variables  # noqa: E402

DISCORD_ERROR = None
try:
    import discord  # noqa: E402
    from commands.artist import (  # noqa: E402
        ARTIST_VIEW_TIMEOUT_SECONDS,
        ArtistSortView,
        _artist_header_text,
        setup_artist_command,
    )
except Exception as exc:  # pragma: no cover - dependency-limited local runs
    DISCORD_ERROR = exc


class DisplayNormalizationTests(unittest.TestCase):
    def test_genres_are_case_insensitive_unique_and_display_capitalised(self):
        self.assertEqual(
            display_genres(["Jazz Pop", "jazz pop", "R&B", "r&b", "EDM"]),
            ["Jazz Pop", "R&B", "EDM"],
        )

    def test_release_dates_use_one_polish_format(self):
        self.assertEqual(display_release_date("2021-01-01"), "01.01.2021")
        self.assertEqual(display_release_date("January 1, 2021"), "01.01.2021")
        self.assertEqual(display_release_date("2021"), "2021")

    def test_release_variables_normalize_date_and_genres_for_all_commands(self):
        variables = build_release_variables(
            {"artist": "Artist", "album": "Album"},
            {
                "release_date": "2024-10-25",
                "genres": ["Jazz Pop", "jazz pop", "Progressive Pop"],
            },
        )
        self.assertEqual(variables.release_date, "25.10.2024")
        self.assertEqual(variables.genres_text, "Jazz Pop, Progressive Pop")


class LastFMParserTests(unittest.TestCase):
    def test_open_graph_artist_image_is_extracted(self):
        class Response:
            status_code = 200
            text = '<meta content="https://lastfm.example/image.jpg" property="og:image">'

            def raise_for_status(self):
                return None

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        self.assertEqual(
            fetch_artist_image("Fievel Is Glauque", session=Session()),
            "https://lastfm.example/image.jpg",
        )


class ArtistImageDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kotone-artist-db-"))
        self.db = Database(
            str(self.tmp / "kotone.sqlite3"),
            monitored_users=("enso",),
            backup_path=str(self.tmp / "kotone.backup.sqlite3"),
        )

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_artist_image_cache_stays_scoped_to_known_artist(self):
        self.db.upsert_rating(
            "enso",
            {"album_id": "1", "artist": "Fievel Is Glauque", "album": "Album"},
        )
        self.assertTrue(
            self.db.save_artist_image(
                "Fievel Is Glauque", "https://lastfm.example/artist.jpg"
            )
        )
        self.assertEqual(
            self.db.get_artist_image("fievel is glauque")["image_url"],
            "https://lastfm.example/artist.jpg",
        )
        self.assertFalse(
            self.db.save_artist_image("Outsider", "https://lastfm.example/no.jpg")
        )


@unittest.skipIf(DISCORD_ERROR is not None, str(DISCORD_ERROR))
class ArtistViewTests(unittest.TestCase):
    def test_artist_embed_has_one_artist_name_and_decade_menu(self):
        discography = {
            "artist": "Fievel Is Glauque",
            "url": "https://example.test/artist",
            "genres": ["Jazz Pop", "jazz pop"],
            "genres_text": "Jazz Pop, jazz pop",
        }
        releases = [
            {
                "album_id": "1",
                "artist": "Fievel Is Glauque",
                "album": "God's Trashmen",
                "url": "https://example.test/album",
                "release_date": "January 1, 2021",
                "year": "2021",
                "album_format": "LP",
                "genres": ["Jazz Pop", "jazz pop"],
            }
        ]
        view = ArtistSortView(discography=discography, releases=releases)
        self.assertEqual(view.timeout, ARTIST_VIEW_TIMEOUT_SECONDS)
        self.assertIn("2020-2029", [option.label for option in view.decade_select.options])
        header = _artist_header_text(discography)
        self.assertNotIn("**Fievel Is Glauque**", header)
        self.assertIn("Jazz Pop", header)

    def test_genre_dropdown_refreshes_its_active_filter_label(self):
        discography = {"artist": "Artist", "url": "https://example.test"}
        releases = [
            {
                "album_id": "1",
                "artist": "Artist",
                "album": "Album",
                "url": "https://example.test/album",
                "year": "2021",
                "album_format": "LP",
                "genres": ["Jazz Pop", "jazz pop"],
            }
        ]
        view = ArtistSortView(
            discography=discography,
            releases=releases,
            selected_genre="Jazz Pop",
        )
        view.refresh_controls()
        self.assertEqual(view.genre_select.placeholder, "Gatunek: Jazz Pop")
        self.assertTrue(
            next(
                option
                for option in view.genre_select.options
                if option.value == "Jazz Pop"
            ).default
        )

    def test_slash_command_only_exposes_artist_argument(self):
        client = discord.Client(intents=discord.Intents.none())
        tree = discord.app_commands.CommandTree(client)
        try:
            setup_artist_command(tree)
            command = next(item for item in tree.get_commands() if item.name == "artist")
            self.assertEqual(
                [parameter.name for parameter in command.parameters],
                ["artist"],
            )
        finally:
            import asyncio

            asyncio.run(client.close())


if __name__ == "__main__":
    unittest.main()
