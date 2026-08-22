"""Regression coverage for Last.fm avatar URL selection."""

from __future__ import annotations

import os
import unittest


os.environ.setdefault("DISCORD_TOKEN", "test-token")

from lastfm import _image_url  # noqa: E402


class LastFMImageUrlTests(unittest.TestCase):
    def test_gif_avatar_wins_over_later_static_variant(self):
        images = [
            {"#text": "https://cdn.example/avatar-small.gif"},
            {"#text": "https://cdn.example/avatar-large.png"},
        ]

        self.assertEqual(
            _image_url(images),
            "https://cdn.example/avatar-small.gif",
        )

    def test_static_largest_variant_remains_the_fallback(self):
        images = [
            {"#text": "https://cdn.example/avatar-small.png"},
            {"#text": "https://cdn.example/avatar-large.png"},
        ]

        self.assertEqual(
            _image_url(images),
            "https://cdn.example/avatar-large.png",
        )


if __name__ == "__main__":
    unittest.main()
