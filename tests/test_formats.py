"""Regresje centralnego katalogu formatów Kotone."""

from __future__ import annotations

import unittest

from formats import (
    DEFAULT_RATING_FETCH_LIMITS,
    RATING_FORMATS,
    build_rating_fetch_limits,
    format_key_from_label,
)


class FormatCatalogTests(unittest.TestCase):
    def test_every_format_has_the_same_stable_shape(self):
        self.assertEqual(set(RATING_FORMATS), set(DEFAULT_RATING_FETCH_LIMITS))
        for key, info in RATING_FORMATS.items():
            self.assertTrue(key)
            self.assertTrue(info["slug"])
            self.assertTrue(info["label"])

    def test_label_slug_and_key_resolve_to_one_canonical_key(self):
        self.assertEqual(format_key_from_label("Music Video"), "music_video")
        self.assertEqual(format_key_from_label("music-video"), "music_video")
        self.assertEqual(format_key_from_label("dj_mix"), "dj_mix")
        self.assertIsNone(format_key_from_label("Bootleg"))

    def test_config_limits_are_complete_and_ignore_invalid_values(self):
        limits = build_rating_fetch_limits(
            {"music-video": "7", "lp": "invalid", "unknown": 999}
        )
        self.assertEqual(set(limits), set(RATING_FORMATS))
        self.assertEqual(limits["music_video"], 7)
        self.assertEqual(limits["lp"], DEFAULT_RATING_FETCH_LIMITS["lp"])


if __name__ == "__main__":
    unittest.main()
