"""Regressions for manually pasted release tracklists."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="kotone-dbmanual-runtime-"))
sys.path.insert(0, str(ROOT))

from commands.dbmanual import _parse_aoty_links, _parse_tracklist  # noqa: E402


class DBManualTracklistTests(unittest.TestCase):
    def test_manual_link_field_accepts_only_a_label_url(self):
        self.assertEqual(
            _parse_aoty_links(
                "label=https://example.test/label;"
                "genres=https://example.test/genre/1, https://example.test/genre/2;"
                "secondary=https://example.test/genre/3"
            ),
            {
                "label_url": "https://example.test/label",
            },
        )

    def test_aoty_rows_are_saved_as_tracks_with_scores_separate_from_titles(self):
        tracks = _parse_tracklist(
            "1\t[She Looked Like Me!](https://example.test/song/1)3:13\t89\n"
            "2\t[Killing Time](https://example.test/song/2)3:53\t93"
        )
        self.assertEqual(
            tracks,
            [
                {
                    "number": 1,
                    "title": "She Looked Like Me!",
                    "url": "https://example.test/song/1",
                    "duration": "3:13",
                    "user_score": "89",
                    "disc": None,
                },
                {
                    "number": 2,
                    "title": "Killing Time",
                    "url": "https://example.test/song/2",
                    "duration": "3:53",
                    "user_score": "93",
                    "disc": None,
                },
            ],
        )

    def test_plain_tracklist_and_a_separate_scores_field_are_joined_by_position(self):
        tracks = _parse_tracklist(
            "1. First track\n2. Second track",
            "90, 89",
        )
        self.assertEqual([track["title"] for track in tracks], ["First track", "Second track"])
        self.assertEqual([track["user_score"] for track in tracks], ["90", "89"])

    def test_scores_need_one_value_for_each_track(self):
        with self.assertRaisesRegex(ValueError, "Liczba track_scores"):
            _parse_tracklist("1. First\n2. Second", "90")
