"""Offline checks for durable AOTY/MusicBrainz search aliases."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = tempfile.mkdtemp(prefix="kotone-artist-alias-runtime-")
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ["DATA_DIR"] = TEST_RUNTIME
sys.path.insert(0, str(ROOT))

from database import Database  # noqa: E402


class ArtistAliasDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="kotone-artist-alias-"))
        self.db = Database(
            str(self.tmp / "kotone.sqlite3"),
            monitored_users=("enso",),
        )
        self.db.upsert_rating(
            "enso",
            {
                "album_id": "123",
                "artist": "Sheena Ringo",
                "album": "Kalk Samen Chestnut Flower",
                "score": "90",
            },
        )
        self.db.save_release_details(
            "123",
            {
                "artist": "Sheena Ringo",
                "album": "Kalk Samen Chestnut Flower",
                "source": "aoty",
            },
        )

    def tearDown(self) -> None:
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_artist_alias_resolves_to_one_cached_aoty_artist(self) -> None:
        self.assertTrue(
            self.db.save_artist_aliases(
                "Sheena Ringo",
                ["Shiina Ringo", "椎名林檎", "Sheena Ringo"],
                source="musicbrainz",
            )
        )
        self.assertEqual(
            self.db.resolve_cached_artist_alias("Shiina Ringo"),
            "Sheena Ringo",
        )
        aliases = self.db.cached_artist_aliases()
        self.assertEqual(
            [item["alias"] for item in aliases if item["artist"] == "Sheena Ringo"].count(
                "Sheena Ringo"
            ),
            1,
        )

    def test_release_group_alias_resolves_to_existing_aoty_album_id(self) -> None:
        self.assertTrue(
            self.db.save_release_source_data(
                "123",
                "musicbrainz",
                {"release_group_aliases": ["加爾基 精液 栗ノ花"]},
                quality="exact-release-group",
            )
        )
        self.assertEqual(
            self.db.resolve_cached_release_alias(
                "Sheena Ringo", "加爾基 精液 栗ノ花"
            ),
            "123",
        )
