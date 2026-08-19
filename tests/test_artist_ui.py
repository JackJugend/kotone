"""Offline regression coverage for the SQLite-first /artist view."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kotone-artist-runtime-")

from database import Database  # noqa: E402
from display_utils import (  # noqa: E402
    display_genres,
    display_release_date,
    display_romanized_name,
)
import lastfm  # noqa: E402
from lastfm import fetch_artist_image  # noqa: E402
from shared import aoty_score_value, build_release_variables, score_value_or_nr  # noqa: E402

DISCORD_ERROR = None
try:
    import discord  # noqa: E402
    from commands.artist import (  # noqa: E402
        ARTIST_VIEW_TIMEOUT_SECONDS,
        MAX_ARTIST_RELEASES,
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

    def test_public_aoty_nr_requires_an_explicit_zero_rating_count(self):
        self.assertEqual(aoty_score_value(None, "0"), "NR")
        self.assertEqual(aoty_score_value(None, "12"), "—")
        self.assertEqual(aoty_score_value(None, None), "—")
        self.assertEqual(aoty_score_value("83", "12"), "83")
        self.assertEqual(aoty_score_value("78.2", "12"), "78")

    def test_personal_missing_score_is_always_nr(self):
        self.assertEqual(score_value_or_nr(None), "NR")
        self.assertEqual(score_value_or_nr("—"), "NR")
        self.assertEqual(score_value_or_nr("90"), "90")

    def test_romanization_hides_original_non_latin_name_in_display(self):
        self.assertEqual(
            display_romanized_name("長谷川白紙 [Hakushi Hasegawa]"),
            "Hakushi Hasegawa",
        )

    def test_must_hear_cover_uses_the_durable_cache_url_for_its_token(self):
        """A stale compact-card URL must not make the badge endpoint return 404."""

        with patch.dict(
            os.environ,
            {"RAILWAY_PUBLIC_DOMAIN": "kotone.example"},
            clear=False,
        ):
            variables = build_release_variables(
                {
                    "album_id": "42",
                    "cover": "https://cdn.albumoftheyear.org/old.jpg",
                    "artist": "Artist",
                    "album": "Album",
                },
                {
                    "album_id": "42",
                    "cover": "https://cdn.albumoftheyear.org/current.jpg",
                    "user_score": "84",
                    "ratings_count": "2,204",
                    "critic_score": "74",
                    "critic_reviews_count": "18",
                    "fetched_at": 1,
                },
            )

        self.assertTrue(variables.must_hear)
        self.assertIn("/must-hear-cover/42/", str(variables.cover))
        from must_hear import cover_token

        self.assertTrue(
            str(variables.cover).endswith(
                f"/{cover_token('42', 'https://cdn.albumoftheyear.org/current.jpg')}.png"
            )
        )


class LastFMParserTests(unittest.TestCase):
    def test_artist_image_comes_from_documented_api_client(self):
        """No command path may silently fall back to scraping Last.fm HTML."""

        with patch.object(
            lastfm.LASTFM,
            "artist_info",
            return_value={"image_url": "https://lastfm.example/image.jpg"},
        ) as artist_info:
            self.assertEqual(
                fetch_artist_image("Fievel Is Glauque"),
                "https://lastfm.example/image.jpg",
            )
        artist_info.assert_called_once_with("Fievel Is Glauque")


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
    def test_artist_embed_has_one_artist_name_and_period_menu(self):
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
        labels = [option.label for option in view.period_select.options]
        self.assertIn("2021", labels)
        self.assertIn("2020-2029", labels)
        header = _artist_header_text(discography)
        self.assertNotIn("**Fievel Is Glauque**", header)
        self.assertIn("Jazz Pop", header)
        self.assertNotIn("Genre:", header)

    def test_artist_header_has_balanced_markdown(self):
        header = _artist_header_text(
            {
                "artist_user_score": None,
                "artist_ratings_count": "0",
                "artist_followers": "0",
                "genres": ["Dream Pop"],
            }
        )
        self.assertNotIn("**  •  **", header)
        self.assertIn("**0 ratings • 0 followers**", header)

    def test_artist_pagination_only_appears_when_releases_need_two_pages(self):
        discography = {"artist": "Artist", "url": "https://example.test"}
        releases = [
            {
                "album_id": str(index),
                "artist": "Artist",
                "album": f"Album {index}",
                "url": f"https://example.test/{index}",
                "year": "2021",
                "album_format": "LP",
                "user_score": "80",
            }
            for index in range(MAX_ARTIST_RELEASES + 1)
        ]
        view = ArtistSortView(discography=discography, releases=releases)
        button_ids = {
            child.custom_id for child in view.children if isinstance(child, discord.ui.Button)
        }
        self.assertIn("artist_previous_page", button_ids)
        self.assertIn("artist_next_page", button_ids)
        self.assertTrue(view.previous_page_button.disabled)
        self.assertFalse(view.next_page_button.disabled)
        self.assertEqual(view.score_desc_button.label, "Ocena ↓")
        self.assertEqual(view.title_asc_button.label, "A–Z")
        self.assertEqual(view.newest_button.label, "Najnowsze")

        one_page = ArtistSortView(
            discography=discography,
            releases=releases[:MAX_ARTIST_RELEASES],
        )
        one_page_ids = {
            child.custom_id
            for child in one_page.children
            if isinstance(child, discord.ui.Button)
        }
        self.assertNotIn("artist_previous_page", one_page_ids)
        self.assertNotIn("artist_next_page", one_page_ids)

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

    def test_slash_command_exposes_artist_and_aoty_score_range(self):
        client = discord.Client(intents=discord.Intents.none())
        tree = discord.app_commands.CommandTree(client)
        try:
            setup_artist_command(tree)
            command = next(item for item in tree.get_commands() if item.name == "artist")
            self.assertEqual(
                [parameter.name for parameter in command.parameters],
                ["artist", "aoty_min", "aoty_max"],
            )
            self.assertFalse(command.parameters[0].required)
        finally:
            import asyncio

            asyncio.run(client.close())


if __name__ == "__main__":
    unittest.main()
